"""Schema validation tests for data/scenarios/*.json files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SCENARIOS_DIR = Path(__file__).parent.parent.parent / "data" / "scenarios"

REQUIRED_TOP_LEVEL = {
    "scenario_id",
    "failure_class",
    "description",
    "alert",
    "known_root_cause",
    "ground_truth",
    "logs",
    "deploys",
}

REQUIRED_ALERT_FIELDS = {"source", "service", "metric", "threshold", "current_value", "severity"}

REQUIRED_ROOT_CAUSE_FIELDS = {"type", "deploy_id", "commit", "description"}

REQUIRED_GROUND_TRUTH_FIELDS = {
    "severity",
    "affected_service",
    "root_cause_summary",
    "recommended_action",
    "deploy_id",
}

REQUIRED_LOG_FIELDS = {"ts", "level", "service", "message"}

REQUIRED_DEPLOY_FIELDS = {"id", "service", "ts", "author", "commit", "files_changed"}

VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

VALID_ALERT_SOURCES = {"datadog", "pagerduty"}

VALID_ALERT_SEVERITIES = {"critical", "high", "warning", "low", "info"}

SCENARIO_IDS = [
    "bad_deploy_01",
    "bad_deploy_02",
    "bad_deploy_03",
    "db_pool_01",
    "db_pool_02",
    "downstream_outage_01",
    "downstream_outage_02",
    "memory_leak_01",
    "memory_leak_02",
    "config_regression_01",
]

VALID_FAILURE_CLASSES = {
    "bad_deploy",
    "db_pool",
    "downstream_outage",
    "memory_leak",
    "config_regression",
}


def load_scenario(scenario_id: str) -> dict:
    path = SCENARIOS_DIR / f"{scenario_id}.json"
    assert path.exists(), f"Scenario file not found: {path}"
    with path.open() as f:
        return json.load(f)  # type: ignore[no-any-return]


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_scenario_file_exists(scenario_id: str) -> None:
    assert (SCENARIOS_DIR / f"{scenario_id}.json").exists()


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_scenario_top_level_fields(scenario_id: str) -> None:
    data = load_scenario(scenario_id)
    missing = REQUIRED_TOP_LEVEL - set(data.keys())
    assert not missing, f"{scenario_id} missing fields: {missing}"


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_scenario_id_matches_filename(scenario_id: str) -> None:
    data = load_scenario(scenario_id)
    assert data["scenario_id"] == scenario_id


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_alert_structure(scenario_id: str) -> None:
    data = load_scenario(scenario_id)
    alert = data["alert"]
    missing = REQUIRED_ALERT_FIELDS - set(alert.keys())
    assert not missing, f"{scenario_id} alert missing: {missing}"
    assert alert["source"] in VALID_ALERT_SOURCES
    assert alert["severity"] in VALID_ALERT_SEVERITIES
    assert isinstance(alert["threshold"], (int, float))
    assert isinstance(alert["current_value"], (int, float))
    assert alert["current_value"] > alert["threshold"], "Alert should fire above threshold"


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_known_root_cause_structure(scenario_id: str) -> None:
    data = load_scenario(scenario_id)
    rc = data["known_root_cause"]
    missing = REQUIRED_ROOT_CAUSE_FIELDS - set(rc.keys())
    assert not missing, f"{scenario_id} known_root_cause missing: {missing}"
    assert isinstance(rc["description"], str) and len(rc["description"]) > 0


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_ground_truth_structure(scenario_id: str) -> None:
    data = load_scenario(scenario_id)
    gt = data["ground_truth"]
    missing = REQUIRED_GROUND_TRUTH_FIELDS - set(gt.keys())
    assert not missing, f"{scenario_id} ground_truth missing: {missing}"
    assert gt["severity"] in {"P1", "P2", "P3", "P4"}
    assert gt["recommended_action"] in {"rollback", "hotfix", "scale", "restart", "escalate"}


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_logs_non_empty(scenario_id: str) -> None:
    data = load_scenario(scenario_id)
    assert len(data["logs"]) >= 5, f"{scenario_id} should have at least 5 log entries"


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_log_entry_structure(scenario_id: str) -> None:
    data = load_scenario(scenario_id)
    for i, entry in enumerate(data["logs"]):
        missing = REQUIRED_LOG_FIELDS - set(entry.keys())
        assert not missing, f"{scenario_id} log[{i}] missing: {missing}"
        level = entry["level"]
        assert level in VALID_LOG_LEVELS, f"{scenario_id} log[{i}] invalid level: {level}"
        assert isinstance(entry["message"], str) and len(entry["message"]) > 0


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_logs_contain_error_signal(scenario_id: str) -> None:
    data = load_scenario(scenario_id)
    error_logs = [e for e in data["logs"] if e["level"] in {"ERROR", "CRITICAL"}]
    assert len(error_logs) >= 2, f"{scenario_id} should have at least 2 ERROR/CRITICAL log entries"


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_deploys_non_empty(scenario_id: str) -> None:
    data = load_scenario(scenario_id)
    assert len(data["deploys"]) >= 1, f"{scenario_id} should have at least 1 deploy entry"


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_deploy_entry_structure(scenario_id: str) -> None:
    data = load_scenario(scenario_id)
    for i, deploy in enumerate(data["deploys"]):
        missing = REQUIRED_DEPLOY_FIELDS - set(deploy.keys())
        assert not missing, f"{scenario_id} deploy[{i}] missing: {missing}"
        assert isinstance(deploy["files_changed"], int) and deploy["files_changed"] >= 0


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_failure_class_is_valid(scenario_id: str) -> None:
    data = load_scenario(scenario_id)
    assert data["failure_class"] in VALID_FAILURE_CLASSES


# ── Scenario-specific assertions ──────────────────────────────────────────────


def test_bad_deploy_01_has_suspect_deploy() -> None:
    data = load_scenario("bad_deploy_01")
    assert data["known_root_cause"]["deploy_id"] == "deploy-abc123"
    assert data["ground_truth"]["deploy_id"] == "deploy-abc123"


def test_bad_deploy_01_culprit_in_deploys() -> None:
    data = load_scenario("bad_deploy_01")
    deploy_ids = {d["id"] for d in data["deploys"]}
    assert "deploy-abc123" in deploy_ids


def test_bad_deploy_02_has_suspect_deploy() -> None:
    data = load_scenario("bad_deploy_02")
    assert data["known_root_cause"]["deploy_id"] == "deploy-usr-447"
    assert data["ground_truth"]["affected_service"] == "user-service"


def test_db_pool_01_no_suspect_deploy() -> None:
    data = load_scenario("db_pool_01")
    assert data["known_root_cause"]["deploy_id"] is None
    assert data["ground_truth"]["deploy_id"] is None
    assert data["ground_truth"]["recommended_action"] == "scale"


def test_db_pool_01_alert_is_latency() -> None:
    data = load_scenario("db_pool_01")
    assert data["alert"]["metric"] == "p99_latency_ms"


def test_scenario_services_are_realistic() -> None:
    expected_services = {
        "bad_deploy_01": "api-gateway",
        "bad_deploy_02": "user-service",
        "bad_deploy_03": "auth-service",
        "db_pool_01": "payment-service",
        "db_pool_02": "order-service",
        "downstream_outage_01": "payment-service",
        "downstream_outage_02": "api-gateway",
        "memory_leak_01": "analytics-pipeline",
        "memory_leak_02": "notification-service",
        "config_regression_01": "user-service",
    }
    for scenario_id, expected_service in expected_services.items():
        data = load_scenario(scenario_id)
        assert data["alert"]["service"] == expected_service, (
            f"{scenario_id}: expected alert service '{expected_service}'"
        )


# ── New scenario-specific assertions ─────────────────────────────────────────


def test_bad_deploy_03_missing_env_var() -> None:
    data = load_scenario("bad_deploy_03")
    assert data["known_root_cause"]["deploy_id"] == "deploy-auth-112"
    assert data["ground_truth"]["recommended_action"] == "rollback"
    assert data["failure_class"] == "bad_deploy"


def test_db_pool_02_has_deploy_culprit() -> None:
    data = load_scenario("db_pool_02")
    assert data["known_root_cause"]["deploy_id"] == "deploy-order-041"
    assert data["ground_truth"]["recommended_action"] == "hotfix"


def test_downstream_outage_01_no_deploy_culprit() -> None:
    data = load_scenario("downstream_outage_01")
    assert data["known_root_cause"]["deploy_id"] is None
    assert data["ground_truth"]["recommended_action"] == "escalate"
    assert data["failure_class"] == "downstream_outage"


def test_downstream_outage_02_alert_on_gateway_root_in_auth() -> None:
    data = load_scenario("downstream_outage_02")
    assert data["alert"]["service"] == "api-gateway"
    assert data["ground_truth"]["affected_service"] == "auth-service"
    assert data["ground_truth"]["deploy_id"] == "deploy-auth-114"


def test_memory_leak_01_is_gradual_oom() -> None:
    data = load_scenario("memory_leak_01")
    assert data["alert"]["metric"] == "memory_usage_bytes"
    assert data["ground_truth"]["recommended_action"] == "rollback"
    assert data["failure_class"] == "memory_leak"


def test_memory_leak_02_alert_is_restarts() -> None:
    data = load_scenario("memory_leak_02")
    assert data["alert"]["metric"] == "process_restarts_total"
    assert data["ground_truth"]["deploy_id"] == "deploy-notif-019"


def test_config_regression_no_deploy_id() -> None:
    data = load_scenario("config_regression_01")
    assert data["known_root_cause"]["deploy_id"] is None
    assert data["ground_truth"]["deploy_id"] is None
    assert data["ground_truth"]["recommended_action"] == "escalate"
    assert data["failure_class"] == "config_regression"


def test_all_10_scenarios_have_unique_ids() -> None:
    seen: set[str] = set()
    for scenario_id in SCENARIO_IDS:
        data = load_scenario(scenario_id)
        sid = data["scenario_id"]
        assert sid not in seen, f"Duplicate scenario_id: {sid}"
        seen.add(sid)
    assert len(seen) == 10


def test_failure_classes_cover_all_4_types() -> None:
    classes = {load_scenario(s)["failure_class"] for s in SCENARIO_IDS}
    assert "bad_deploy" in classes
    assert "db_pool" in classes
    assert "downstream_outage" in classes
    assert "memory_leak" in classes
    assert "config_regression" in classes


def test_escalate_scenarios_have_no_deploy_id() -> None:
    for scenario_id in SCENARIO_IDS:
        data = load_scenario(scenario_id)
        if data["ground_truth"]["recommended_action"] == "escalate":
            assert data["ground_truth"]["deploy_id"] is None, (
                f"{scenario_id}: escalate scenarios should not have a deploy_id"
            )
