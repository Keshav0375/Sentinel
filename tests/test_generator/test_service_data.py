"""Schema and consistency validation tests for data/services/*.json files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SERVICES_DIR = Path(__file__).parent.parent.parent / "data" / "services"
SCENARIOS_DIR = Path(__file__).parent.parent.parent / "data" / "scenarios"


def load_json(filename: str) -> dict:
    path = SERVICES_DIR / filename
    assert path.exists(), f"Missing data file: {path}"
    with path.open() as f:
        return json.load(f)  # type: ignore[no-any-return]


def load_all_scenarios() -> list[dict]:
    return [
        json.loads(p.read_text())
        for p in sorted(SCENARIOS_DIR.glob("*.json"))
    ]


VALID_TIERS = {"critical", "standard", "best-effort"}
VALID_PROTOCOLS = {"http", "db-read", "grpc"}
VALID_CRITICALITIES = {"critical", "standard", "best-effort"}


# ── service_map.json ──────────────────────────────────────────────────────────


def test_service_map_file_exists() -> None:
    assert (SERVICES_DIR / "service_map.json").exists()


def test_service_map_has_services_key() -> None:
    data = load_json("service_map.json")
    assert "services" in data
    assert isinstance(data["services"], list)


def test_service_map_has_8_services() -> None:
    data = load_json("service_map.json")
    assert len(data["services"]) == 8


def test_service_map_required_fields() -> None:
    data = load_json("service_map.json")
    required = {"name", "team", "tier", "oncall_channel", "repo_url", "description", "dependencies"}
    for svc in data["services"]:
        missing = required - set(svc.keys())
        assert not missing, f"Service '{svc.get('name')}' missing fields: {missing}"


def test_service_map_tiers_are_valid() -> None:
    data = load_json("service_map.json")
    for svc in data["services"]:
        assert svc["tier"] in VALID_TIERS, (
            f"Service '{svc['name']}' has invalid tier: {svc['tier']}"
        )


def test_service_map_unique_names() -> None:
    data = load_json("service_map.json")
    names = [svc["name"] for svc in data["services"]]
    assert len(names) == len(set(names)), "Duplicate service names found"


def test_service_map_oncall_channels_format() -> None:
    data = load_json("service_map.json")
    for svc in data["services"]:
        assert svc["oncall_channel"].startswith("#"), (
            f"Service '{svc['name']}' oncall_channel should start with '#'"
        )


def test_service_map_repo_urls_are_github() -> None:
    data = load_json("service_map.json")
    for svc in data["services"]:
        assert svc["repo_url"].startswith("https://github.com/"), (
            f"Service '{svc['name']}' repo_url should be a GitHub URL"
        )


def test_service_map_dependencies_reference_known_services() -> None:
    data = load_json("service_map.json")
    all_names = {svc["name"] for svc in data["services"]}
    for svc in data["services"]:
        for dep in svc["dependencies"]:
            assert dep in all_names, (
                f"Service '{svc['name']}' has unknown dependency: '{dep}'"
            )


def test_service_map_contains_scenario_services() -> None:
    data = load_json("service_map.json")
    service_names = {svc["name"] for svc in data["services"]}
    required = {
        "api-gateway", "user-service", "auth-service",
        "payment-service", "order-service", "notification-service",
        "analytics-pipeline", "cdn-proxy",
    }
    missing = required - service_names
    assert not missing, f"Missing expected services: {missing}"


def test_critical_services_have_identity() -> None:
    data = load_json("service_map.json")
    critical = [s for s in data["services"] if s["tier"] == "critical"]
    assert len(critical) >= 5, "Expected at least 5 critical-tier services"


# ── dependency_graph.json ─────────────────────────────────────────────────────


def test_dependency_graph_file_exists() -> None:
    assert (SERVICES_DIR / "dependency_graph.json").exists()


def test_dependency_graph_has_edges_key() -> None:
    data = load_json("dependency_graph.json")
    assert "edges" in data
    assert isinstance(data["edges"], list)


def test_dependency_graph_required_fields() -> None:
    data = load_json("dependency_graph.json")
    required = {"from", "to", "protocol", "criticality"}
    for i, edge in enumerate(data["edges"]):
        missing = required - set(edge.keys())
        assert not missing, f"Edge[{i}] missing fields: {missing}"


def test_dependency_graph_edges_reference_known_services() -> None:
    svc_data = load_json("service_map.json")
    graph_data = load_json("dependency_graph.json")
    all_names = {svc["name"] for svc in svc_data["services"]}
    for edge in graph_data["edges"]:
        assert edge["from"] in all_names, f"Unknown 'from' service: {edge['from']}"
        assert edge["to"] in all_names, f"Unknown 'to' service: {edge['to']}"


def test_dependency_graph_protocols_are_valid() -> None:
    data = load_json("dependency_graph.json")
    for edge in data["edges"]:
        assert edge["protocol"] in VALID_PROTOCOLS, (
            f"Invalid protocol: {edge['protocol']}"
        )


def test_dependency_graph_no_self_loops() -> None:
    data = load_json("dependency_graph.json")
    for edge in data["edges"]:
        assert edge["from"] != edge["to"], (
            f"Self-loop detected: {edge['from']} -> {edge['to']}"
        )


def test_dependency_graph_no_duplicate_edges() -> None:
    data = load_json("dependency_graph.json")
    pairs = [(e["from"], e["to"]) for e in data["edges"]]
    assert len(pairs) == len(set(pairs)), "Duplicate edges found in dependency graph"


def test_auth_service_in_api_gateway_deps() -> None:
    data = load_json("dependency_graph.json")
    gw_to_auth = any(
        e["from"] == "api-gateway" and e["to"] == "auth-service"
        for e in data["edges"]
    )
    assert gw_to_auth, "api-gateway must depend on auth-service (critical path)"


def test_order_service_depends_on_payment() -> None:
    data = load_json("dependency_graph.json")
    assert any(
        e["from"] == "order-service" and e["to"] == "payment-service"
        for e in data["edges"]
    )


# ── runbooks.json ─────────────────────────────────────────────────────────────


def test_runbooks_file_exists() -> None:
    assert (SERVICES_DIR / "runbooks.json").exists()


def test_runbooks_has_runbooks_key() -> None:
    data = load_json("runbooks.json")
    assert "runbooks" in data
    assert isinstance(data["runbooks"], list)


def test_runbooks_required_fields() -> None:
    data = load_json("runbooks.json")
    required = {"id", "service_name", "failure_class", "steps"}
    for rb in data["runbooks"]:
        missing = required - set(rb.keys())
        assert not missing, f"Runbook '{rb.get('id')}' missing fields: {missing}"


def test_runbooks_steps_are_non_empty_lists() -> None:
    data = load_json("runbooks.json")
    for rb in data["runbooks"]:
        assert isinstance(rb["steps"], list), f"Runbook '{rb['id']}' steps must be a list"
        assert len(rb["steps"]) >= 3, f"Runbook '{rb['id']}' needs at least 3 steps"


def test_runbooks_unique_ids() -> None:
    data = load_json("runbooks.json")
    ids = [rb["id"] for rb in data["runbooks"]]
    assert len(ids) == len(set(ids)), "Duplicate runbook IDs found"


def test_runbooks_service_names_match_service_map() -> None:
    svc_data = load_json("service_map.json")
    rb_data = load_json("runbooks.json")
    all_names = {svc["name"] for svc in svc_data["services"]}
    for rb in rb_data["runbooks"]:
        assert rb["service_name"] in all_names, (
            f"Runbook '{rb['id']}' references unknown service: {rb['service_name']}"
        )


def test_runbooks_cover_scenario_failure_classes() -> None:
    rb_data = load_json("runbooks.json")
    scenarios = load_all_scenarios()

    # Every scenario's affected service + failure class should have a runbook
    covered = {(rb["service_name"], rb["failure_class"]) for rb in rb_data["runbooks"]}
    for scenario in scenarios:
        service = scenario["ground_truth"]["affected_service"]
        fc = scenario["failure_class"]
        assert (service, fc) in covered, (
            f"No runbook for service='{service}' failure_class='{fc}' "
            f"(needed by scenario '{scenario['scenario_id']}')"
        )


def test_runbooks_steps_are_strings() -> None:
    data = load_json("runbooks.json")
    for rb in data["runbooks"]:
        for i, step in enumerate(rb["steps"]):
            assert isinstance(step, str) and len(step) > 0, (
                f"Runbook '{rb['id']}' step[{i}] is not a non-empty string"
            )


# ── Cross-file consistency ────────────────────────────────────────────────────


def test_service_map_deps_consistent_with_graph() -> None:
    svc_data = load_json("service_map.json")
    graph_data = load_json("dependency_graph.json")
    graph_edges = {(e["from"], e["to"]) for e in graph_data["edges"]}

    for svc in svc_data["services"]:
        for dep in svc["dependencies"]:
            assert (svc["name"], dep) in graph_edges, (
                f"Service '{svc['name']}' lists '{dep}' as dependency "
                f"but edge is missing from dependency_graph.json"
            )


@pytest.mark.parametrize("service_name", [
    "api-gateway", "user-service", "auth-service",
    "payment-service", "order-service", "notification-service",
    "analytics-pipeline", "cdn-proxy",
])
def test_each_service_has_at_least_one_runbook(service_name: str) -> None:
    rb_data = load_json("runbooks.json")
    service_runbooks = [rb for rb in rb_data["runbooks"] if rb["service_name"] == service_name]
    assert len(service_runbooks) >= 1, f"No runbooks defined for service '{service_name}'"
