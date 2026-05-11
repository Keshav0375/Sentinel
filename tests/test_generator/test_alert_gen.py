"""Tests for sentinel.generator.scenarios and sentinel.generator.alert_gen."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from sentinel.generator.alert_gen import generate_alert, list_scenarios, load_scenario
from sentinel.generator.scenarios import (
    GroundTruth,
    KnownRootCause,
    Scenario,
    ScenarioAlert,
    ScenarioDeploy,
    ScenarioLogEntry,
)
from sentinel.models.alert import AlertPayload, AlertSeverity, AlertSource
from sentinel.models.incident import Severity
from sentinel.models.log_entry import LogLevel
from sentinel.models.remediation import RemediationAction

SCENARIOS_DIR = Path(__file__).parent.parent.parent / "data" / "scenarios"

ALL_SCENARIO_IDS = [
    "bad_deploy_01",
    "bad_deploy_02",
    "bad_deploy_03",
    "config_regression_01",
    "db_pool_01",
    "db_pool_02",
    "downstream_outage_01",
    "downstream_outage_02",
    "memory_leak_01",
    "memory_leak_02",
]


# ── ScenarioAlert ─────────────────────────────────────────────────────────────


def test_scenario_alert_valid() -> None:
    alert = ScenarioAlert(
        source=AlertSource.DATADOG,
        service="api-gateway",
        metric="error_rate",
        threshold=0.05,
        current_value=0.34,
        severity=AlertSeverity.CRITICAL,
    )
    assert alert.source is AlertSource.DATADOG
    assert alert.severity is AlertSeverity.CRITICAL


def test_scenario_alert_invalid_source_raises() -> None:
    with pytest.raises(ValidationError):
        ScenarioAlert(
            source="splunk",  # type: ignore[arg-type]
            service="svc",
            metric="m",
            threshold=0.1,
            current_value=0.5,
            severity=AlertSeverity.HIGH,
        )


# ── KnownRootCause ────────────────────────────────────────────────────────────


def test_known_root_cause_nullable_fields() -> None:
    rc = KnownRootCause(
        type="downstream_outage",
        deploy_id=None,
        commit=None,
        description="Stripe API is down",
    )
    assert rc.deploy_id is None
    assert rc.commit is None


# ── GroundTruth ───────────────────────────────────────────────────────────────


def test_ground_truth_valid() -> None:
    gt = GroundTruth(
        severity=Severity.P1,
        affected_service="api-gateway",
        root_cause_summary="Missing config key",
        recommended_action=RemediationAction.ROLLBACK,
        deploy_id="deploy-abc123",
    )
    assert gt.severity is Severity.P1
    assert gt.recommended_action is RemediationAction.ROLLBACK


def test_ground_truth_invalid_severity_raises() -> None:
    with pytest.raises(ValidationError):
        GroundTruth(
            severity="P5",  # type: ignore[arg-type]
            affected_service="svc",
            root_cause_summary="r",
            recommended_action=RemediationAction.ROLLBACK,
            deploy_id=None,
        )


# ── ScenarioLogEntry ──────────────────────────────────────────────────────────


def test_scenario_log_entry_valid() -> None:
    entry = ScenarioLogEntry(
        ts="2026-05-10T03:00:01Z",  # type: ignore[arg-type]
        level=LogLevel.ERROR,
        service="api-gateway",
        message="NullPointerException in AuthMiddleware",
    )
    assert entry.level is LogLevel.ERROR
    assert entry.trace_id is None


def test_scenario_log_entry_invalid_level_raises() -> None:
    with pytest.raises(ValidationError):
        ScenarioLogEntry(
            ts="2026-05-10T03:00:01Z",  # type: ignore[arg-type]
            level="VERBOSE",  # type: ignore[arg-type]
            service="svc",
            message="msg",
        )


# ── ScenarioDeploy ────────────────────────────────────────────────────────────


def test_scenario_deploy_nullable_commit() -> None:
    deploy = ScenarioDeploy(
        id="deploy-cfg-001",
        service="user-service",
        ts="2026-05-14T13:55:00Z",  # type: ignore[arg-type]
        author="ops-team",
        commit=None,
        files_changed=0,
        description="ConfigMap update — no code change",
    )
    assert deploy.commit is None


# ── load_scenario ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("scenario_id", ALL_SCENARIO_IDS)
def test_load_scenario_returns_scenario(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    assert isinstance(scenario, Scenario)
    assert scenario.scenario_id == scenario_id


@pytest.mark.parametrize("scenario_id", ALL_SCENARIO_IDS)
def test_load_scenario_has_logs(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    assert len(scenario.logs) >= 5


@pytest.mark.parametrize("scenario_id", ALL_SCENARIO_IDS)
def test_load_scenario_log_levels_are_enum(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    for entry in scenario.logs:
        assert isinstance(entry.level, LogLevel)


@pytest.mark.parametrize("scenario_id", ALL_SCENARIO_IDS)
def test_load_scenario_ground_truth_types(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    assert isinstance(scenario.ground_truth.severity, Severity)
    assert isinstance(scenario.ground_truth.recommended_action, RemediationAction)


def test_load_scenario_not_found_raises() -> None:
    with pytest.raises(FileNotFoundError):
        load_scenario("nonexistent_scenario_999", scenarios_dir=SCENARIOS_DIR)


def test_load_scenario_bad_deploy_01() -> None:
    scenario = load_scenario("bad_deploy_01", scenarios_dir=SCENARIOS_DIR)
    assert scenario.failure_class == "bad_deploy"
    assert scenario.alert.service == "api-gateway"
    assert scenario.ground_truth.severity is Severity.P1
    assert scenario.ground_truth.recommended_action is RemediationAction.ROLLBACK
    assert scenario.known_root_cause.deploy_id == "deploy-abc123"


def test_load_scenario_db_pool_01_no_deploy() -> None:
    scenario = load_scenario("db_pool_01", scenarios_dir=SCENARIOS_DIR)
    assert scenario.known_root_cause.deploy_id is None
    assert scenario.ground_truth.deploy_id is None
    assert scenario.ground_truth.recommended_action is RemediationAction.SCALE


def test_load_scenario_config_regression_escalate() -> None:
    scenario = load_scenario("config_regression_01", scenarios_dir=SCENARIOS_DIR)
    assert scenario.ground_truth.recommended_action is RemediationAction.ESCALATE
    assert scenario.known_root_cause.deploy_id is None


def test_load_scenario_downstream_outage_02_split_service() -> None:
    scenario = load_scenario("downstream_outage_02", scenarios_dir=SCENARIOS_DIR)
    assert scenario.alert.service == "api-gateway"
    assert scenario.ground_truth.affected_service == "auth-service"


# ── generate_alert ────────────────────────────────────────────────────────────


def test_generate_alert_returns_alert_payload() -> None:
    scenario = load_scenario("bad_deploy_01", scenarios_dir=SCENARIOS_DIR)
    alert = generate_alert(scenario)
    assert isinstance(alert, AlertPayload)


def test_generate_alert_fields_match_scenario() -> None:
    scenario = load_scenario("bad_deploy_01", scenarios_dir=SCENARIOS_DIR)
    alert = generate_alert(scenario)
    assert alert.source is AlertSource.DATADOG
    assert alert.service == "api-gateway"
    assert alert.metric == "error_rate"
    assert alert.threshold == pytest.approx(0.05)
    assert alert.current_value == pytest.approx(0.34)
    assert alert.severity is AlertSeverity.CRITICAL


def test_generate_alert_has_unique_alert_id() -> None:
    scenario = load_scenario("bad_deploy_01", scenarios_dir=SCENARIOS_DIR)
    a1 = generate_alert(scenario)
    a2 = generate_alert(scenario)
    assert a1.alert_id != a2.alert_id


def test_generate_alert_metadata_contains_scenario_id() -> None:
    scenario = load_scenario("bad_deploy_02", scenarios_dir=SCENARIOS_DIR)
    alert = generate_alert(scenario)
    assert alert.metadata["scenario_id"] == "bad_deploy_02"
    assert alert.metadata["failure_class"] == "bad_deploy"


def test_generate_alert_timestamp_is_set() -> None:
    scenario = load_scenario("db_pool_01", scenarios_dir=SCENARIOS_DIR)
    alert = generate_alert(scenario)
    assert alert.timestamp is not None
    assert alert.timestamp.tzinfo is not None


@pytest.mark.parametrize("scenario_id", ALL_SCENARIO_IDS)
def test_generate_alert_all_scenarios(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    alert = generate_alert(scenario)
    assert alert.service == scenario.alert.service
    assert alert.severity is scenario.alert.severity


# ── list_scenarios ────────────────────────────────────────────────────────────


def test_list_scenarios_returns_all_10(tmp_path: Path) -> None:
    assert len(list_scenarios(scenarios_dir=SCENARIOS_DIR)) == 10


def test_list_scenarios_sorted() -> None:
    ids = list_scenarios(scenarios_dir=SCENARIOS_DIR)
    assert ids == sorted(ids)


def test_list_scenarios_contains_expected_ids() -> None:
    ids = list_scenarios(scenarios_dir=SCENARIOS_DIR)
    for expected in ALL_SCENARIO_IDS:
        assert expected in ids


def test_list_scenarios_empty_dir(tmp_path: Path) -> None:
    assert list_scenarios(scenarios_dir=tmp_path) == []


def test_list_scenarios_nonexistent_dir(tmp_path: Path) -> None:
    result = list_scenarios(scenarios_dir=tmp_path / "does_not_exist")
    assert result == []
