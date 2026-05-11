"""Deploy history generator — produces deploy list including culprit and distractors."""

from __future__ import annotations

from sentinel.generator.scenarios import Scenario
from sentinel.models.deploy import Deploy

_MISSING_COMMIT = "n/a"


def generate_deploys(scenario: Scenario) -> list[Deploy]:
    """Convert scenario deploy records to Deploy model objects.

    Maps ScenarioDeploy field names to Deploy field names:
      ts        → timestamp
      commit    → commit_sha  (None becomes "n/a" — config-only changes
                               have no associated commit SHA)

    Returns deploys sorted by timestamp descending (most recent first),
    matching the order a real deployment system returns history.

    Args:
        scenario: Loaded and validated Scenario.

    Returns:
        List of Deploy objects sorted most-recent-first.
    """
    deploys = [
        Deploy(
            id=d.id,
            service=d.service,
            timestamp=d.ts,
            author=d.author,
            commit_sha=d.commit if d.commit is not None else _MISSING_COMMIT,
            files_changed=d.files_changed,
            description=d.description,
        )
        for d in scenario.deploys
    ]
    deploys.sort(key=lambda d: d.timestamp, reverse=True)
    return deploys
