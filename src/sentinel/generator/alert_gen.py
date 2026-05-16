"""Alert payload generator — produces AlertPayload from a scenario."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sentinel.generator.scenarios import Scenario
from sentinel.models.alert import AlertPayload

# Default location: <project_root>/data/scenarios/
# parents[0]=generator, parents[1]=sentinel, parents[2]=src, parents[3]=project root
_DEFAULT_SCENARIOS_DIR = Path(__file__).resolve().parents[3] / "data" / "scenarios"


def load_scenario(
    scenario_id: str,
    scenarios_dir: Path | None = None,
) -> Scenario:
    """Load and validate a scenario from its JSON file.

    Args:
        scenario_id: The scenario_id string (e.g. "bad_deploy_01"). The file
            is expected at <scenarios_dir>/<scenario_id>.json.
        scenarios_dir: Override the default scenarios directory. Useful in
            tests and when running from a non-standard working directory.

    Raises:
        FileNotFoundError: If the scenario file does not exist.
        pydantic.ValidationError: If the JSON does not match the Scenario schema.
    """
    base = scenarios_dir if scenarios_dir is not None else _DEFAULT_SCENARIOS_DIR
    path = base / f"{scenario_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Scenario file not found: {path}")
    return Scenario.model_validate_json(path.read_text(encoding="utf-8"))


def generate_alert(scenario: Scenario) -> AlertPayload:
    """Create a runtime AlertPayload from a loaded Scenario.

    alert_id is generated fresh (UUID4). Timestamp is derived from the
    scenario's last log entry so the LLM's time-windowed log queries
    actually match the scenario data.

    Args:
        scenario: A fully-loaded and validated Scenario object.

    Returns:
        AlertPayload ready to POST to the webhook receiver.
    """
    alert_ts = scenario.logs[-1].ts if scenario.logs else datetime.now(UTC)
    return AlertPayload(
        source=scenario.alert.source,
        service=scenario.alert.service,
        metric=scenario.alert.metric,
        threshold=scenario.alert.threshold,
        current_value=scenario.alert.current_value,
        severity=scenario.alert.severity,
        timestamp=alert_ts,
        metadata={
            "scenario_id": scenario.scenario_id,
            "failure_class": scenario.failure_class,
        },
    )


def list_scenarios(scenarios_dir: Path | None = None) -> list[str]:
    """Return a sorted list of available scenario IDs.

    Scans the scenarios directory for *.json files and returns their
    stem names (filename without extension) in alphabetical order.

    Args:
        scenarios_dir: Override the default scenarios directory.

    Returns:
        Sorted list of scenario_id strings.
    """
    base = scenarios_dir if scenarios_dir is not None else _DEFAULT_SCENARIOS_DIR
    if not base.exists():
        return []
    return sorted(p.stem for p in base.glob("*.json"))
