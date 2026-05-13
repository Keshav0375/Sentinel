"""Tests for scripts/run_scenario.py — argument handling and helper functions."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from sentinel.memory.short_term import ShortTermMemory
from sentinel.models.alert import AlertPayload, AlertSeverity, AlertSource
from sentinel.models.incident import IncidentStatus, TimelineEntry

# ── Import the script module ──────────────────────────────────────────────────
# scripts/ is not on the test Python path — load via importlib.
_SCRIPT_PATH = Path(__file__).parent.parent.parent / "scripts" / "run_scenario.py"


def _load_script() -> object:
    spec = importlib.util.spec_from_file_location("run_scenario_script", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_script = _load_script()
_print_header = _script._print_header  # type: ignore[attr-defined]
_print_timeline = _script._print_timeline  # type: ignore[attr-defined]
_usage = _script._usage  # type: ignore[attr-defined]
main = _script.main  # type: ignore[attr-defined]
run_scenario = _script.run_scenario  # type: ignore[attr-defined]

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_stm_with_incident(incident_id: str = "inc-test0001") -> ShortTermMemory:
    stm = ShortTermMemory()
    alert = AlertPayload(
        source=AlertSource.DATADOG,
        service="api-gateway",
        metric="error_rate",
        threshold=0.05,
        current_value=0.34,
        severity=AlertSeverity.CRITICAL,
    )
    stm.create(incident_id, alert)
    return stm


# ── _print_header ─────────────────────────────────────────────────────────────


def test_print_header_does_not_crash(capsys: pytest.CaptureFixture[str]) -> None:
    _print_header("Test Section")
    captured = capsys.readouterr()
    assert "Test Section" in captured.out


def test_print_header_contains_separator(capsys: pytest.CaptureFixture[str]) -> None:
    _print_header("Anything")
    captured = capsys.readouterr()
    assert "=" in captured.out


# ── _print_timeline ───────────────────────────────────────────────────────────


def test_print_timeline_empty(capsys: pytest.CaptureFixture[str]) -> None:
    stm = _make_stm_with_incident()
    _print_timeline(stm, "inc-test0001")
    captured = capsys.readouterr()
    assert "no timeline entries" in captured.out.lower()


def test_print_timeline_with_entries(capsys: pytest.CaptureFixture[str]) -> None:
    stm = _make_stm_with_incident()
    entry = TimelineEntry(
        agent_name="triage_agent",
        action="get_service_metadata",
        result_summary="api-gateway: critical tier",
    )
    stm.add_timeline_entry("inc-test0001", entry)
    _print_timeline(stm, "inc-test0001")
    captured = capsys.readouterr()
    assert "triage_agent" in captured.out
    assert "get_service_metadata" in captured.out


def test_print_timeline_truncates_long_summary(capsys: pytest.CaptureFixture[str]) -> None:
    stm = _make_stm_with_incident()
    long_summary = "x" * 200
    entry = TimelineEntry(
        agent_name="log_analyst_agent",
        action="fetch_logs",
        result_summary=long_summary,
    )
    stm.add_timeline_entry("inc-test0001", entry)
    _print_timeline(stm, "inc-test0001")
    captured = capsys.readouterr()
    # Output line should not contain 200 x's
    assert "x" * 200 not in captured.out
    assert "..." in captured.out


def test_print_timeline_shows_status(capsys: pytest.CaptureFixture[str]) -> None:
    stm = _make_stm_with_incident()
    stm.update_context("inc-test0001", status=IncidentStatus.INVESTIGATING)
    _print_timeline(stm, "inc-test0001")
    captured = capsys.readouterr()
    assert "investigating" in captured.out


def test_print_timeline_missing_incident(capsys: pytest.CaptureFixture[str]) -> None:
    stm = ShortTermMemory()
    _print_timeline(stm, "inc-does-not-exist")
    captured = capsys.readouterr()
    assert "cleared" in captured.out.lower() or "cleared" in captured.out


def test_print_timeline_multiple_entries(capsys: pytest.CaptureFixture[str]) -> None:
    stm = _make_stm_with_incident()
    for i in range(3):
        stm.add_timeline_entry(
            "inc-test0001",
            TimelineEntry(
                agent_name=f"agent_{i}",
                action=f"action_{i}",
                result_summary=f"result {i}",
            ),
        )
    _print_timeline(stm, "inc-test0001")
    captured = capsys.readouterr()
    for i in range(3):
        assert f"action_{i}" in captured.out


# ── _usage ────────────────────────────────────────────────────────────────────


def test_usage_prints_something(capsys: pytest.CaptureFixture[str]) -> None:
    _usage()
    captured = capsys.readouterr()
    assert "Usage" in captured.out
    assert "run_scenario.py" in captured.out


def test_usage_mentions_list_flag(capsys: pytest.CaptureFixture[str]) -> None:
    _usage()
    captured = capsys.readouterr()
    assert "--list" in captured.out


def test_usage_mentions_example_scenario(capsys: pytest.CaptureFixture[str]) -> None:
    _usage()
    captured = capsys.readouterr()
    assert "bad_deploy_01" in captured.out


# ── main() argument handling ──────────────────────────────────────────────────


def test_main_no_args_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(sys, "argv", ["run_scenario.py"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
    assert exc_info.value.code == 1


def test_main_help_exits_0() -> None:
    with patch.object(sys, "argv", ["run_scenario.py", "--help"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
    assert exc_info.value.code == 0


def test_main_list_flag_prints_scenarios(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(sys, "argv", ["run_scenario.py", "--list"]):
        main()  # should not raise
    captured = capsys.readouterr()
    # At least one scenario should be listed (we have 10 scenario files)
    assert "bad_deploy_01" in captured.out


def test_main_unknown_scenario_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(sys, "argv", ["run_scenario.py", "nonexistent_scenario_xyz"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Error" in captured.err or "nonexistent" in captured.err.lower()


def test_main_unknown_scenario_lists_available(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(sys, "argv", ["run_scenario.py", "totally_fake_scenario_abc"]):
        with pytest.raises(SystemExit):
            main()
    captured = capsys.readouterr()
    assert "bad_deploy_01" in captured.err


# ── run_scenario — structural tests ──────────────────────────────────────────


def test_run_scenario_raises_file_not_found_for_unknown_id() -> None:
    """run_scenario raises FileNotFoundError immediately for unknown scenarios."""
    import asyncio

    with pytest.raises(FileNotFoundError):
        asyncio.run(run_scenario("totally_unknown_scenario_xyz"))


def test_run_scenario_is_async() -> None:
    import inspect

    assert inspect.iscoroutinefunction(run_scenario)


def test_script_file_exists() -> None:
    assert _SCRIPT_PATH.exists(), f"Script not found: {_SCRIPT_PATH}"


def test_script_has_main_guard() -> None:
    content = _SCRIPT_PATH.read_text(encoding="utf-8")
    assert 'if __name__ == "__main__"' in content


def test_available_scenarios_non_empty() -> None:
    from sentinel.generator.alert_gen import list_scenarios

    scenarios = list_scenarios()
    assert len(scenarios) >= 10, f"Expected at least 10 scenarios, got {len(scenarios)}"


def test_load_bad_deploy_01_scenario() -> None:
    """Verify the most common smoke test scenario loads without error."""
    from sentinel.generator.alert_gen import generate_alert, load_scenario

    scenario = load_scenario("bad_deploy_01")
    assert scenario.scenario_id == "bad_deploy_01"
    assert scenario.failure_class == "bad_deploy"

    alert = generate_alert(scenario)
    assert alert.service == scenario.alert.service
    assert alert.metric == scenario.alert.metric


def test_load_db_pool_01_scenario() -> None:
    from sentinel.generator.alert_gen import load_scenario

    scenario = load_scenario("db_pool_01")
    assert scenario.scenario_id == "db_pool_01"
    assert scenario.ground_truth.affected_service


def test_load_downstream_outage_01_scenario() -> None:
    from sentinel.generator.alert_gen import load_scenario

    scenario = load_scenario("downstream_outage_01")
    assert scenario.ground_truth.deploy_id is None  # outage, no deploy culprit
