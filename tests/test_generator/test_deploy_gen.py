"""Tests for sentinel.generator.deploy_gen."""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.generator.alert_gen import load_scenario
from sentinel.generator.deploy_gen import generate_deploys
from sentinel.models.deploy import Deploy

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


# ── Return type and basic contract ───────────────────────────────────────────


@pytest.mark.parametrize("scenario_id", ALL_SCENARIO_IDS)
def test_generate_deploys_returns_deploy_list(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    deploys = generate_deploys(scenario)
    assert isinstance(deploys, list)
    assert all(isinstance(d, Deploy) for d in deploys)


@pytest.mark.parametrize("scenario_id", ALL_SCENARIO_IDS)
def test_generate_deploys_count_matches_scenario(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    deploys = generate_deploys(scenario)
    assert len(deploys) == len(scenario.deploys)


# ── Ordering ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("scenario_id", ALL_SCENARIO_IDS)
def test_generate_deploys_sorted_most_recent_first(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    deploys = generate_deploys(scenario)
    if len(deploys) > 1:
        for i in range(len(deploys) - 1):
            assert deploys[i].timestamp >= deploys[i + 1].timestamp, (
                f"{scenario_id}: deploys not sorted most-recent-first"
            )


# ── Field mapping ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("scenario_id", ALL_SCENARIO_IDS)
def test_generate_deploys_ids_preserved(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    deploys = generate_deploys(scenario)
    scenario_ids = {d.id for d in scenario.deploys}
    result_ids = {d.id for d in deploys}
    assert scenario_ids == result_ids


@pytest.mark.parametrize("scenario_id", ALL_SCENARIO_IDS)
def test_generate_deploys_timestamp_from_ts(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    deploys = generate_deploys(scenario)
    scenario_ts = {d.ts for d in scenario.deploys}
    result_ts = {d.timestamp for d in deploys}
    assert scenario_ts == result_ts


@pytest.mark.parametrize("scenario_id", ALL_SCENARIO_IDS)
def test_generate_deploys_commit_sha_never_none(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    deploys = generate_deploys(scenario)
    for deploy in deploys:
        assert deploy.commit_sha is not None
        assert len(deploy.commit_sha) > 0


@pytest.mark.parametrize("scenario_id", ALL_SCENARIO_IDS)
def test_generate_deploys_authors_preserved(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    deploys = generate_deploys(scenario)
    scenario_authors = {d.author for d in scenario.deploys}
    result_authors = {d.author for d in deploys}
    assert scenario_authors == result_authors


@pytest.mark.parametrize("scenario_id", ALL_SCENARIO_IDS)
def test_generate_deploys_files_changed_preserved(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    deploys = generate_deploys(scenario)
    scenario_fc = {d.files_changed for d in scenario.deploys}
    result_fc = {d.files_changed for d in deploys}
    assert scenario_fc == result_fc


def test_generate_deploys_null_commit_becomes_na() -> None:
    # All scenario deploys currently have non-null commits, but we test
    # the Scenario model directly with a null commit to verify the fallback.
    from sentinel.generator.scenarios import (
        GroundTruth,
        KnownRootCause,
        Scenario,
        ScenarioAlert,
        ScenarioDeploy,
        ScenarioLogEntry,
    )
    from sentinel.models.alert import AlertSeverity, AlertSource
    from sentinel.models.incident import Severity
    from sentinel.models.remediation import RemediationAction

    null_commit_deploy = ScenarioDeploy(
        id="deploy-cfg-001",
        service="user-service",
        ts="2026-05-14T13:55:00Z",  # type: ignore[arg-type]
        author="ops-team",
        commit=None,
        files_changed=0,
        description="ConfigMap update only",
    )
    scenario = Scenario(
        scenario_id="test_null_commit",
        failure_class="config_regression",
        description="test",
        alert=ScenarioAlert(
            source=AlertSource.DATADOG,
            service="user-service",
            metric="error_rate",
            threshold=0.03,
            current_value=0.48,
            severity=AlertSeverity.HIGH,
        ),
        known_root_cause=KnownRootCause(
            type="config_regression",
            deploy_id=None,
            commit=None,
            description="feature flag enabled",
        ),
        ground_truth=GroundTruth(
            severity=Severity.P2,
            affected_service="user-service",
            root_cause_summary="flag bug",
            recommended_action=RemediationAction.ESCALATE,
            deploy_id=None,
        ),
        logs=[
            ScenarioLogEntry(
                ts="2026-05-14T13:55:00Z",  # type: ignore[arg-type]
                level="ERROR",  # type: ignore[arg-type]
                service="user-service",
                message="test error",
            )
        ],
        deploys=[null_commit_deploy],
    )
    deploys = generate_deploys(scenario)
    assert len(deploys) == 1
    assert deploys[0].commit_sha == "n/a"


# ── Scenario-specific assertions ──────────────────────────────────────────────


def test_bad_deploy_01_most_recent_is_culprit() -> None:
    scenario = load_scenario("bad_deploy_01", scenarios_dir=SCENARIOS_DIR)
    deploys = generate_deploys(scenario)
    # deploy-abc123 was deployed at 02:44:50, which is the most recent
    assert deploys[0].id == "deploy-abc123"


def test_db_pool_01_no_recent_deploy_is_first() -> None:
    scenario = load_scenario("db_pool_01", scenarios_dir=SCENARIOS_DIR)
    deploys = generate_deploys(scenario)
    # db_pool_01 has no culprit deploy; most recent is from order-service
    assert len(deploys) == 3
    # Verify sorting: most recent should be the order-service deploy from 08:00
    assert deploys[0].timestamp > deploys[1].timestamp


def test_generate_deploys_timestamps_tz_aware() -> None:
    scenario = load_scenario("bad_deploy_02", scenarios_dir=SCENARIOS_DIR)
    for deploy in generate_deploys(scenario):
        assert deploy.timestamp.tzinfo is not None
