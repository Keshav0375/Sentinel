"""Tests for sentinel.generator.log_gen."""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.generator.alert_gen import load_scenario
from sentinel.generator.log_gen import generate_logs
from sentinel.models.log_entry import LogEntry, LogLevel

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
def test_generate_logs_returns_log_entries(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    logs = generate_logs(scenario, noise=False)
    assert isinstance(logs, list)
    assert all(isinstance(e, LogEntry) for e in logs)


@pytest.mark.parametrize("scenario_id", ALL_SCENARIO_IDS)
def test_generate_logs_without_noise_count(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    logs = generate_logs(scenario, noise=False)
    assert len(logs) == len(scenario.logs)


@pytest.mark.parametrize("scenario_id", ALL_SCENARIO_IDS)
def test_generate_logs_with_noise_has_more_entries(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    without_noise = generate_logs(scenario, noise=False)
    with_noise = generate_logs(scenario, noise=True)
    assert len(with_noise) > len(without_noise)


# ── Timestamp mapping ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("scenario_id", ALL_SCENARIO_IDS)
def test_generate_logs_sorted_by_timestamp(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    for noise in (False, True):
        logs = generate_logs(scenario, noise=noise)
        timestamps = [e.timestamp for e in logs]
        assert timestamps == sorted(timestamps), f"{scenario_id} logs not sorted (noise={noise})"


@pytest.mark.parametrize("scenario_id", ALL_SCENARIO_IDS)
def test_generate_logs_timestamps_are_tz_aware(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    for entry in generate_logs(scenario, noise=False):
        assert entry.timestamp.tzinfo is not None


# ── Signal entries preserved ──────────────────────────────────────────────────


@pytest.mark.parametrize("scenario_id", ALL_SCENARIO_IDS)
def test_generate_logs_all_scenario_messages_present(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    logs = generate_logs(scenario, noise=True)
    log_messages = {e.message for e in logs}
    for entry in scenario.logs:
        assert entry.message in log_messages, (
            f"{scenario_id}: scenario message missing from output: {entry.message[:60]}"
        )


@pytest.mark.parametrize("scenario_id", ALL_SCENARIO_IDS)
def test_generate_logs_scenario_services_present(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    logs = generate_logs(scenario, noise=False)
    log_services = {e.service for e in logs}
    scenario_services = {e.service for e in scenario.logs}
    assert scenario_services == log_services


@pytest.mark.parametrize("scenario_id", ALL_SCENARIO_IDS)
def test_generate_logs_levels_are_log_level_enum(scenario_id: str) -> None:
    scenario = load_scenario(scenario_id, scenarios_dir=SCENARIOS_DIR)
    for entry in generate_logs(scenario, noise=True):
        assert isinstance(entry.level, LogLevel)


# ── Noise properties ──────────────────────────────────────────────────────────


def test_noise_entries_are_info_level() -> None:
    scenario = load_scenario("bad_deploy_01", scenarios_dir=SCENARIOS_DIR)
    without = set(e.message for e in generate_logs(scenario, noise=False))
    with_noise = generate_logs(scenario, noise=True)
    noise_entries = [e for e in with_noise if e.message not in without]
    assert all(e.level is LogLevel.INFO for e in noise_entries)


def test_noise_entries_not_from_alert_service() -> None:
    scenario = load_scenario("bad_deploy_01", scenarios_dir=SCENARIOS_DIR)
    without = set(e.message for e in generate_logs(scenario, noise=False))
    with_noise = generate_logs(scenario, noise=True)
    noise_entries = [e for e in with_noise if e.message not in without]
    alert_service = scenario.alert.service
    assert all(e.service != alert_service for e in noise_entries), (
        "Noise entries must not come from the alert service"
    )


def test_noise_entries_not_from_affected_service() -> None:
    scenario = load_scenario("downstream_outage_02", scenarios_dir=SCENARIOS_DIR)
    # This scenario: alert on api-gateway, root cause in auth-service
    without = set(e.message for e in generate_logs(scenario, noise=False))
    with_noise = generate_logs(scenario, noise=True)
    noise_entries = [e for e in with_noise if e.message not in without]
    excluded = {scenario.alert.service, scenario.ground_truth.affected_service}
    for entry in noise_entries:
        assert entry.service not in excluded, f"Noise service '{entry.service}' should be excluded"


def test_noise_timestamps_within_scenario_window() -> None:
    scenario = load_scenario("bad_deploy_01", scenarios_dir=SCENARIOS_DIR)
    without = set(e.message for e in generate_logs(scenario, noise=False))
    logs_no_noise = generate_logs(scenario, noise=False)
    with_noise = generate_logs(scenario, noise=True)
    noise_entries = [e for e in with_noise if e.message not in without]

    start = logs_no_noise[0].timestamp
    end = logs_no_noise[-1].timestamp
    for entry in noise_entries:
        assert start <= entry.timestamp <= end, (
            f"Noise entry timestamp {entry.timestamp} outside scenario window"
        )


def test_generate_logs_deterministic() -> None:
    scenario = load_scenario("bad_deploy_01", scenarios_dir=SCENARIOS_DIR)
    run1 = generate_logs(scenario, noise=True)
    run2 = generate_logs(scenario, noise=True)
    assert [e.message for e in run1] == [e.message for e in run2]
    assert [e.service for e in run1] == [e.service for e in run2]


def test_generate_logs_no_noise_flag() -> None:
    scenario = load_scenario("db_pool_01", scenarios_dir=SCENARIOS_DIR)
    no_noise = generate_logs(scenario, noise=False)
    # Without noise, only scenario services appear
    scenario_services = {e.service for e in scenario.logs}
    result_services = {e.service for e in no_noise}
    assert result_services == scenario_services
